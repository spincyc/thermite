"""Shared code for the openHAB sandbox, boot-check and smoke tools.

Everything here runs as the invoking user with the Python standard library.
The runtime is the openHAB distribution zip on a Temurin JRE, both pinned in
versions.json and cached under build/sandbox/.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import oh_cli
import oh_logging

from oh_paths import SITE as AREA
VAR = AREA / "var"
#: Keys a test instance's rules read, as $OPENHAB_SECRETS (production's are
#: in its state, oh_deploy.State.secrets).
SECRETS = VAR / "secrets"
BUILD = AREA / "build"
CACHE = BUILD / "sandbox"
DOWNLOADS = CACHE / "downloads"
VERSIONS_FILE = AREA / "versions.json"

KARAF_USER = "openhab"
# The distribution's stock Karaf console credentials (userdata/etc/users.properties).
# The console only listens on 127.0.0.1, and sandbox userdata never leaves var/.
KARAF_PASSWORD = "habopen"
ADMIN_USER = "admin"
TOKEN_NAME = "automation"
TOKEN_SCOPE = "sandbox"

ADDON_CATEGORIES = ("automation", "binding", "misc", "persistence", "transformation", "ui", "voice")

# Statuses a handler holds while it waits for its device. In the sandbox a
# Sonos can sit in INITIALIZING until the binding's 60s poll, because the
# speakers' UPnP event callbacks cannot reach a web server bound to 127.0.0.1.
PENDING_STATUSES = frozenset({"INITIALIZING", "UNKNOWN"})
# UNINITIALIZED details that mean the handler itself has not arrived yet.
HANDLER_PENDING_DETAILS = ("HANDLER_MISSING_ERROR", "NOT_YET_READY")
FAIL_DETAILS = frozenset({"HANDLER_CONFIGURATION_PENDING", "CONFIGURATION_ERROR"})


class SandboxError(Exception):
    """A failure the tools report as a single message and exit status 2."""


class ChecksumMismatch(SandboxError):
    pass


# --------------------------------------------------------------------------
# versions.json and downloads


def load_versions(path: Path = VERSIONS_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in ("openhab", "java"):
        if key not in data:
            raise SandboxError(f"{path}: missing '{key}'")
    for label, entry in (("openhab.distribution", data["openhab"]["distribution"]), ("java", data["java"])):
        for field in ("url", "sha256"):
            if not entry.get(field):
                raise SandboxError(f"{path}: {label}.{field} is not set")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise SandboxError(f"{path}: {label}.sha256 is not a lowercase hex sha256")
    return data


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_checksum(path: Path, expected: str) -> str:
    """Return the sha256 of path; raise ChecksumMismatch if it is not expected."""
    expected = expected.strip().lower()
    actual = sha256_file(path)
    if actual != expected:
        raise ChecksumMismatch(f"{path}: sha256 {actual} does not match pinned {expected}")
    return actual


def url_basename(url: str) -> str:
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    if not name or name in (".", "..") or "/" in name:
        raise SandboxError(f"cannot derive a file name from {url}")
    return name


def download(url: str, expected_sha256: str, dest_dir: Path = DOWNLOADS, log=print) -> Path:
    """Download url into dest_dir once; verify it against expected_sha256.

    A cached file is reused only if it still matches. A fresh download that
    does not match is deleted and refused.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / url_basename(url)
    if dest.exists():
        try:
            verify_checksum(dest, expected_sha256)
            log(f"cached  {dest.name} (sha256 ok)")
            return dest
        except ChecksumMismatch as exc:
            log(f"cached  {dest.name} does not match its pin, downloading again: {exc}")
            dest.unlink()
    part = dest.with_name(dest.name + ".part")
    log(f"fetch   {url}")
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "homelab-openhab-sandbox"})
    oh_cli.trace_http("GET", url)
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    try:
        verify_checksum(part, expected_sha256)
    except ChecksumMismatch:
        part.unlink()
        raise
    part.rename(dest)
    log(f"fetched {dest.name}: {dest.stat().st_size} bytes in {time.monotonic() - started:.1f}s (sha256 ok)")
    return dest


def _safe_member_path(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise SandboxError(f"archive member escapes the extraction root: {name}")
    return target


def extract_zip(archive: Path, dest: Path) -> None:
    """Extract a zip into dest, keeping Unix permission bits (zipfile drops them)."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = _safe_member_path(dest, info.filename)
            mode = (info.external_attr >> 16) & 0o7777
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SandboxError(f"{archive}: symlink members are not supported: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if info.create_system == 3 and mode:
                os.chmod(target, mode & 0o777)


def extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        tf.extractall(dest, filter="data")


def _install(archive: Path, final: Path, extractor, single_top: bool) -> Path:
    """Extract archive atomically into final; a stamp file marks completion."""
    stamp = final / ".installed-from"
    digest = sha256_file(archive)
    if stamp.exists() and stamp.read_text().strip() == digest:
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=final.parent))
    try:
        extractor(archive, tmp)
        root = tmp
        if single_top:
            entries = [p for p in tmp.iterdir()]
            if len(entries) != 1 or not entries[0].is_dir():
                raise SandboxError(f"{archive}: expected one top-level directory")
            root = entries[0]
        (root / ".installed-from").write_text(digest + "\n")
        os.chmod(root, 0o755)
        if final.exists():
            shutil.rmtree(final)
        root.rename(final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return final


@dataclasses.dataclass(frozen=True)
class Runtime:
    openhab_version: str
    dist: Path
    java_home: Path

    @property
    def karaf(self) -> Path:
        return self.dist / "runtime" / "bin" / "karaf"

    @property
    def client(self) -> Path:
        return self.dist / "runtime" / "bin" / "client"


def runtime_paths(versions: dict) -> Runtime:
    oh = versions["openhab"]["version"]
    java = versions["java"]["version"]
    return Runtime(oh, CACHE / f"openhab-{oh}", CACHE / f"jre-{java}")


def fetch(versions: dict, log=print) -> Runtime:
    rt = runtime_paths(versions)
    jre_archive = download(versions["java"]["url"], versions["java"]["sha256"], log=log)
    dist_archive = download(
        versions["openhab"]["distribution"]["url"], versions["openhab"]["distribution"]["sha256"], log=log
    )
    started = time.monotonic()
    _install(jre_archive, rt.java_home, extract_tar, single_top=True)
    _install(dist_archive, rt.dist, extract_zip, single_top=False)
    for script in (rt.karaf, rt.client, rt.java_home / "bin" / "java"):
        if not os.access(script, os.X_OK):
            raise SandboxError(f"{script} is not executable after extraction")
    log(f"ready   {rt.dist} on {rt.java_home} ({time.monotonic() - started:.1f}s to verify/extract)")
    return rt


def require_runtime(versions: dict) -> Runtime:
    rt = runtime_paths(versions)
    for path in (rt.dist / ".installed-from", rt.java_home / ".installed-from"):
        if not path.exists():
            raise SandboxError(f"{path.parent} is not installed; run bin/thermite sandbox fetch")
    return rt


# --------------------------------------------------------------------------
# path safety


def is_disposable(path: Path, root: Path = VAR) -> bool:
    """True if path lies strictly below root after resolving symlinks."""
    try:
        resolved = Path(path).resolve()
        base = Path(root).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved != base and base in resolved.parents


def wipe_dir(path: Path, root: Path = VAR) -> None:
    """Delete path and its contents, refusing anything not strictly below root."""
    if not is_disposable(path, root):
        raise SandboxError(f"refusing to empty {path}: it is not below {root}")
    if Path(path).is_symlink():
        raise SandboxError(f"refusing to empty {path}: it is a symlink")
    if Path(path).exists():
        shutil.rmtree(path)


def mirror_conf(source: Path, dest: Path, transform) -> Path:
    """Make dest (below var/) hold source's files passed through transform,
    a function of {relative path: text, or bytes for a file that is not
    UTF-8}: write what differs and delete what is no longer there, so a
    running openHAB picks up only real changes."""
    source, dest = Path(source).resolve(), Path(dest)
    if not source.is_dir():
        raise SandboxError(f"conf directory {source} does not exist")
    if not is_disposable(dest) or dest.is_symlink():
        raise SandboxError(f"refusing to mirror conf into {dest}: it is not a directory below {VAR}")
    files = transform({p.relative_to(source).as_posix(): _conf_file(p.read_bytes())
                       for p in sorted(source.rglob("*")) if p.is_file()})
    present = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()} if dest.is_dir() else set()
    for path in sorted(present - set(files)):
        (dest / path).unlink()
    for path, body in files.items():
        target = dest / path
        data = body.encode("utf-8") if isinstance(body, str) else body
        if path not in present or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return dest.resolve()


def _conf_file(data: bytes) -> str | bytes:
    """A conf file as text, or as bytes when it is not UTF-8 (an image)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


# --------------------------------------------------------------------------
# addons.cfg


def parse_addons_cfg(text: str) -> dict[str, list[str]]:
    """Parse openHAB's services/addons.cfg into {category: [addon, ...]}.

    Accepts `key = a, b`, optional `org.openhab.addons:` PID prefixes and
    backslash continuation lines. Unknown keys (remote, includeIncompatible,
    suggestionFinder*) are ignored.
    """
    result: dict[str, list[str]] = {}
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buffer and (not line or line.startswith("#") or line.startswith("!")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1]
            continue
        logical.append(buffer + line)
        buffer = ""
    if buffer:
        logical.append(buffer)
    for line in logical:
        sep = re.search(r"[=:]", line)
        key, value = line, ""
        if "=" in line:
            key, value = line.split("=", 1)
        elif sep:
            key, value = line.split(sep.group(0), 1)
        key = key.strip()
        if ":" in key:
            key = key.rsplit(":", 1)[1].strip()
        if key not in ADDON_CATEGORIES:
            continue
        names = [n.strip() for n in value.split(",") if n.strip()]
        if names:
            result[key] = names
    return result


def addon_uids(requested: dict[str, list[str]]) -> list[str]:
    return [f"{cat}-{name}" for cat in ADDON_CATEGORIES for name in requested.get(cat, [])]


def read_requested_addons(conf: Path) -> dict[str, list[str]]:
    cfg = Path(conf) / "services" / "addons.cfg"
    if not cfg.exists():
        return {}
    return parse_addons_cfg(cfg.read_text(encoding="utf-8"))


def installed_addon_uids(addons: list[dict]) -> set[str]:
    uids = set()
    for addon in addons:
        if not addon.get("installed"):
            continue
        uid = addon.get("uid") or f"{addon.get('type')}-{addon.get('id')}"
        uids.add(uid)
    return uids


# --------------------------------------------------------------------------
# Thing status classification


def classify_thing(status: str, detail: str | None, require_online: bool = False) -> str:
    """Return 'ok', 'warn' or 'fail' for a Thing's statusInfo."""
    status = (status or "").upper()
    detail = (detail or "NONE").upper()
    if status == "UNINITIALIZED" or detail in FAIL_DETAILS:
        return "fail"
    if status == "ONLINE":
        return "ok"
    if require_online:
        return "fail"
    return "warn"


def thing_pending(thing: dict, devices: bool = True) -> bool:
    """Still waiting for its handler (or start level 30), or with devices=True for its device too."""
    info = thing.get("statusInfo", {})
    if devices and info.get("status") in PENDING_STATUSES:
        return True
    return info.get("status") == "UNINITIALIZED" and info.get("statusDetail") in HANDLER_PENDING_DETAILS


def things_pending(things: list[dict], devices: bool = True) -> list[str]:
    return [t.get("UID", "?") for t in things if thing_pending(t, devices)]


def things_snapshot(things: list[dict]) -> tuple:
    return tuple(sorted(
        (t.get("UID", "?"), t.get("statusInfo", {}).get("status"), t.get("statusInfo", {}).get("statusDetail"))
        for t in things
    ))


# Every Thing ONLINE is the best a boot check can see, so it only needs to hold
# for a couple of polls instead of the full quiet window.
ONLINE_WINDOW = 2.0


class Settler:
    """Follow Thing statuses across polls.

    Settled: none pending and none changed for `window` seconds (ONLINE_WINDOW
    when every Thing is ONLINE). Stalled: some still pending while neither a
    Thing nor the start level changed for `stall` seconds. With
    devices=False only a missing handler counts as pending, so a Thing left
    INITIALIZING/UNKNOWN by its device does not hold the result back.
    """

    def __init__(self, window: float, stall: float | None = None, devices: bool = True):
        self.window = window
        self.stall = stall
        self.devices = devices
        self.snapshot = None
        self.since = None
        self.progress = None
        self.progressed = None
        self.pending = False

    def observe(self, things: list[dict], now: float, level: int | None = None) -> bool:
        snap = things_snapshot(things)
        self.pending = bool(things_pending(things, self.devices))
        if (snap, level) != self.progress:
            self.progress = (snap, level)
            self.progressed = now
        if snap != self.snapshot or self.pending:
            self.snapshot = snap
            self.since = None if self.pending else now
            return False
        if self.since is None:
            self.since = now
        all_online = bool(things) and all(t.get("statusInfo", {}).get("status") == "ONLINE" for t in things)
        return now - self.since >= (min(self.window, ONLINE_WINDOW) if all_online else self.window)

    def stalled(self, now: float) -> bool:
        return (self.stall is not None and self.pending and self.progressed is not None
                and now - self.progressed >= self.stall)


def summarize_things(things: list[dict], require_online: bool = False, allow: frozenset = frozenset()) -> dict:
    by_status: dict[str, int] = {}
    rows = []
    failed = []
    for thing in sorted(things, key=lambda t: t.get("UID", "")):
        info = thing.get("statusInfo", {})
        status = info.get("status", "?")
        detail = info.get("statusDetail", "NONE")
        key = status if detail in (None, "NONE") else f"{status}/{detail}"
        by_status[key] = by_status.get(key, 0) + 1
        verdict = classify_thing(status, detail, require_online)
        if thing.get("UID") in allow and verdict == "fail" and status != "UNINITIALIZED" and detail not in FAIL_DETAILS:
            verdict = "allowed"
        row = {
            "uid": thing.get("UID"),
            "label": thing.get("label"),
            "status": status,
            "detail": detail,
            "description": info.get("description"),
            "verdict": verdict,
        }
        rows.append(row)
        if verdict == "fail":
            failed.append(row)
    return {"count": len(rows), "by_status": dict(sorted(by_status.items())), "things": rows, "failed": failed}


# --------------------------------------------------------------------------
# openhab.log parsing

LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d{3})\s+\[\s*(?P<level>[A-Z]+)\s*\]\s+"
    r"\[(?P<logger>[^\]]*)\]\s+-\s+(?P<msg>.*)$"
)

# openHAB 5.2 wording, e.g.
#   DSL model 'x.things' has errors, therefore ignoring it: [2,1]: missing ']' at 'Thing'
#   YAML model yaml/x.yaml: Failed to process model: while parsing a flow sequence
#   YAML model yaml/x.yaml: invalid item "A": invalid value "B" for "type" field
#   Script loading error, ignoring file '/conf/automation/js/x.js'
# Older releases said "Configuration model" instead of "DSL model".
MODEL_PATTERNS = (
    re.compile(r"\b(DSL|Configuration) model '[^']+' (has errors|is either empty or cannot be parsed)"),
    re.compile(r"^YAML model \S+:"),
    re.compile(r"^Script loading error, ignoring file "),
    # A file's Thing, Item or page refused because one with its UID exists
    # already, such as an Overview page made in Main UI.
    re.compile(r"^Cannot add \"\w+\" with key \"[^\"]+\"\. It exists already from provider "),
)
MODEL_LOGGERS = re.compile(r"ModelRepository|yaml", re.IGNORECASE)
MODEL_WORDS = re.compile(r"\b(error|errors|invalid|failed|cannot|unknown|not supported)\b", re.IGNORECASE)


@dataclasses.dataclass
class LogRecord:
    ts: str
    level: str
    logger: str
    message: str
    lineno: int

    def text(self) -> str:
        return f"{self.ts} [{self.level}] [{self.logger.strip()}] - {self.message}"


def parse_log(text: str) -> list[LogRecord]:
    """Split openhab.log into records; continuation lines join their record."""
    records: list[LogRecord] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = LOG_LINE.match(line)
        if match:
            records.append(
                LogRecord(match["ts"], match["level"], match["logger"].strip(), match["msg"], lineno)
            )
        elif records:
            records[-1].message += "\n" + line
    return records


def is_model_problem(record: LogRecord) -> bool:
    if record.level not in ("WARN", "ERROR"):
        return False
    first = record.message.split("\n", 1)[0]
    if any(p.search(first) for p in MODEL_PATTERNS):
        return True
    return bool(MODEL_LOGGERS.search(record.logger)) and bool(MODEL_WORDS.search(first))


def log_problems(records: list[LogRecord]) -> dict[str, list[LogRecord]]:
    model = [r for r in records if is_model_problem(r)]
    model_ids = {id(r) for r in model}
    errors = [r for r in records if r.level == "ERROR" and id(r) not in model_ids]
    return {"model": model, "errors": errors}


# --------------------------------------------------------------------------
# HTTP


class Http:
    """Minimal REST client. Only GET unless allow_write is set."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10, allow_write: bool = False):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.allow_write = allow_write

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        if method != "GET" and not self.allow_write:
            raise SandboxError(f"refusing {method} {path}: this client is read-only")
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        oh_cli.trace_http(method, self.base + path)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read()
            return resp.status, data

    def get_json(self, path: str):
        status, data = self.request("GET", path)
        return json.loads(data.decode("utf-8")) if data else None

    def try_get_json(self, path: str):
        """Return (http_status, json-or-None); connection failures give status 0."""
        try:
            return 200, self.get_json(path)
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None


def read_token(path: Path | None) -> str | None:
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8").strip()
    return text or None


def write_secret(path: Path, value: str) -> None:
    """Write value to path with mode 0600 from creation onwards."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# instances


LOOPBACK = "127.0.0.1"
#: A live sandbox listens on every interface, so phones on the LAN reach it.
EVERY_INTERFACE = "0.0.0.0"
#: The sandbox's modes: a read-only copy of conf/, or conf/ unchanged.
READ_ONLY = "read-only"
LIVE = "live"


@dataclasses.dataclass
class Instance:
    name: str
    state: Path
    conf: Path
    userdata: Path
    http_port: int
    https_port: int
    ssh_port: int
    http_address: str = LOOPBACK

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def pid_file(self) -> Path:
        return self.state / "openhab.pid"

    @property
    def console_log(self) -> Path:
        return self.state / "console.log"

    @property
    def token_file(self) -> Path:
        return self.state / "api-token"

    @property
    def password_file(self) -> Path:
        return self.state / "admin-password"

    @property
    def openhab_log(self) -> Path:
        return self.userdata / "logs" / "openhab.log"

    @property
    def mode_file(self) -> Path:
        return self.state / "mode"


def sandbox_instance(conf: Path | None = None, userdata: Path | None = None, live: bool = False) -> Instance:
    state = VAR / "sandbox"
    return Instance(
        "sandbox",
        state,
        Path(conf or AREA / "conf").resolve(),
        Path(userdata or state / "userdata").resolve(),
        18080,
        18443,
        18101,
        EVERY_INTERFACE if live else LOOPBACK,
    )


def read_mode(inst: Instance) -> str:
    """The mode the instance was last started in; read-only unless it was live."""
    try:
        return LIVE if inst.mode_file.read_text(encoding="utf-8").strip() == LIVE else READ_ONLY
    except FileNotFoundError:
        return READ_ONLY


def write_mode(inst: Instance, mode: str) -> None:
    inst.state.mkdir(parents=True, exist_ok=True)
    inst.mode_file.write_text(mode + "\n", encoding="utf-8")


def boot_check_instance(conf: Path | None = None) -> Instance:
    state = VAR / "boot-check"
    return Instance(
        "boot-check", state, Path(conf or AREA / "conf").resolve(), state / "userdata", 18081, 18444, 18102
    )


def _cfg_set(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out, done = [], False
    for line in lines:
        if pattern.match(line):
            out.append(f"{key} = {value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key} = {value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def init_userdata(rt: Runtime, inst: Instance, log=print) -> bool:
    """Populate an empty userdata from the distribution template; pin the console port.

    Returns True if userdata was initialised. The Karaf SSH console stays on
    127.0.0.1; only its port changes so instances can run side by side.
    """
    created = False
    inst.userdata.mkdir(parents=True, exist_ok=True)
    if not (inst.userdata / "etc").exists():
        shutil.copytree(rt.dist / "userdata", inst.userdata, dirs_exist_ok=True)
        log(f"init    {inst.userdata} from the distribution template")
        created = True
    shell_cfg = inst.userdata / "etc" / "org.apache.karaf.shell.cfg"
    _cfg_set(shell_cfg, "sshPort", str(inst.ssh_port))
    _cfg_set(shell_cfg, "sshHost", "127.0.0.1")
    (inst.userdata / "etc" / "org.apache.karaf.features.xml").write_text(FEATURES_PROCESSING, encoding="utf-8")
    oh_logging.install(inst.userdata)
    return created


# The LSP model server (org.openhab.core.model.lsp) binds new ServerSocket(5007)
# on every interface and has no address setting. The sandbox has no use for it,
# so it is kept out of the Karaf resolution entirely.
FEATURES_PROCESSING = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Written by bin/thermite sandbox for sandbox userdata; not used in production. -->
<featuresProcessing xmlns="http://karaf.apache.org/xmlns/features-processing/v1.0.0">
    <blacklistedBundles>
        <bundle>mvn:org.openhab.core.bundles/org.openhab.core.model.lsp</bundle>
    </blacklistedBundles>
</featuresProcessing>
"""


# A graceful stop hangs in WebClientFactoryImpl.deactivate (Jetty selector
# stop) for minutes, so instances are stopped with SIGKILL after a short wait.
# Equinox saves its bundle database this long after a change (default
# 30000 ms); at 1000 ms a kill a few seconds after the last add-on install
# loses nothing.
SAVE_DELAY_MS = 1000
STOP_TIMEOUT = 10


def instance_env(rt: Runtime, inst: Instance, conf: Path | None = None, logdir: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENHAB_", "KARAF_", "JAVA_", "EXTRA_JAVA"))}
    env["EXTRA_JAVA_OPTS"] = f"-Declipse.stateSaveDelayInterval={SAVE_DELAY_MS}"
    env.update(
        JAVA_HOME=str(rt.java_home),
        OPENHAB_CONF=str(conf or inst.conf),
        OPENHAB_USERDATA=str(inst.userdata),
        OPENHAB_LOGDIR=str(logdir or inst.userdata / "logs"),
        OPENHAB_BACKUPS=str(inst.state / "backups"),
        OPENHAB_SECRETS=str(SECRETS),
        OPENHAB_HTTP_ADDRESS=inst.http_address,
        OPENHAB_HTTP_PORT=str(inst.http_port),
        OPENHAB_HTTPS_PORT=str(inst.https_port),
        PATH=f"{rt.java_home / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    )
    return env


def _proc_entries():
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            yield int(entry.name), entry


def group_members(pgid: int) -> list[int]:
    members = []
    for pid, entry in _proc_entries():
        try:
            stat_text = (entry / "stat").read_text()
        except OSError:
            continue
        # Fields after the parenthesised command (which may contain spaces):
        # state, ppid, pgrp. Zombies are dead whoever has yet to reap them.
        fields = stat_text.rsplit(")", 1)[1].split()
        if len(fields) > 2 and int(fields[2]) == pgid and fields[0] != "Z":
            members.append(pid)
    return members


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def running_pgid(inst: Instance) -> int | None:
    """The process group of a live openHAB for inst, or None (stale pid files are removed)."""
    if not inst.pid_file.exists():
        return None
    try:
        pgid = int(inst.pid_file.read_text().split()[0])
    except (ValueError, IndexError):
        inst.pid_file.unlink(missing_ok=True)
        return None
    if any("karaf" in cmdline(pid) for pid in group_members(pgid)):
        return pgid
    inst.pid_file.unlink(missing_ok=True)
    return None


class InstanceLock:
    """Exclusive flock on <state>/lock so two callers never drive one instance."""

    def __init__(self, inst: Instance):
        self.path = inst.state / "lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.seek(0)
            holder = self.fh.read().strip() or "?"
            self.fh.close()
            raise SandboxError(f"{self.path} is held by another run (pid {holder})")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"{os.getpid()}\n")
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        self.fh.close()
        return False


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def start_instance(rt: Runtime, inst: Instance, log=print) -> int:
    if running_pgid(inst):
        raise SandboxError(f"{inst.name} is already running (pid file {inst.pid_file})")
    for port in (inst.http_port, inst.https_port, inst.ssh_port):
        if port_in_use(port):
            raise SandboxError(f"port {port} on 127.0.0.1 is already in use")
    inst.state.mkdir(parents=True, exist_ok=True)
    init_userdata(rt, inst, log=log)
    if not inst.conf.is_dir():
        raise SandboxError(f"conf directory {inst.conf} does not exist")
    prime_cache(rt, inst, log=log)
    return _launch(rt, inst, instance_env(rt, inst), "starting")


def _launch(rt: Runtime, inst: Instance, env: dict, what: str) -> int:
    console = open(inst.console_log, "ab")
    console.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {what} {inst.name}\n".encode())
    console.flush()
    oh_cli.trace_run([rt.karaf, "server"], inst.userdata)
    proc = subprocess.Popen(
        [str(rt.karaf), "server"],
        cwd=str(inst.userdata),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=console,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    console.close()
    inst.pid_file.write_text(f"{proc.pid}\n")
    return proc.pid


PRIMED_LEVEL = 10


def prime_cache(rt: Runtime, inst: Instance, log=print, timeout: float = 120) -> bool:
    """Build the OSGi cache of an empty userdata with a throwaway server.

    runtime/bin/karaf does this itself on a first boot (awaitOsgiCacheReady),
    but it starts the real server as soon as the throwaway one reports start
    level 0, while that JVM is still shutting down and writing the cache, and
    the throwaway server already installs the add-ons from conf's addons.cfg.
    In 5.2.3 that race can leave Karaf's feature state listing add-ons as
    installed whose bundles the framework never kept: their Things stay
    UNINITIALIZED/HANDLER_MISSING_ERROR across restarts. Here the throwaway
    server gets a conf without add-ons and must exit completely before the
    real server starts. Holding cache-refresh-lock makes the script skip its
    own priming. Returns True if the cache was primed.
    """
    if (inst.userdata / "cache" / "org.eclipse.osgi").is_dir():
        return False
    prime = inst.state / "prime"
    wipe_dir(prime, root=inst.state)
    (prime / "conf" / "services").mkdir(parents=True)
    (prime / "conf" / "services" / "addons.cfg").write_text("# no add-ons while the OSGi cache is primed\n")
    lock = inst.userdata / "cache-refresh-lock"
    lock.touch()
    level_file = inst.userdata / "openhab-start-level"
    level_file.write_text("0\n")
    started = time.monotonic()
    pgid = None
    try:
        pgid = _launch(rt, inst, instance_env(rt, inst, conf=prime / "conf", logdir=prime / "logs"), "priming cache for")
        deadline = started + timeout
        while (start_level(inst) or 0) < PRIMED_LEVEL:
            if not group_members(pgid):
                raise SandboxError(f"{inst.name}: cache priming server exited; see {inst.console_log}")
            if time.monotonic() >= deadline:
                raise SandboxError(f"{inst.name}: cache priming server did not reach start level "
                                   f"{PRIMED_LEVEL} within {timeout:.0f}s")
            time.sleep(1)
        os.killpg(pgid, signal.SIGTERM)
        end = time.monotonic() + 60
        while group_members(pgid):
            if time.monotonic() >= end:
                raise SandboxError(f"{inst.name}: cache priming server did not exit within 60s of SIGTERM")
            time.sleep(0.5)
        pgid = None
        log(f"prime   OSGi cache built in {time.monotonic() - started:.0f}s")
        return True
    except BaseException:
        if pgid:
            _kill_group(pgid)
        shutil.rmtree(inst.userdata / "cache", ignore_errors=True)
        raise
    finally:
        lock.unlink(missing_ok=True)
        level_file.write_text("0\n")
        inst.pid_file.unlink(missing_ok=True)


def wait_rest(inst: Instance, pgid: int, timeout: float, log=print, min_level: int = 0) -> dict:
    """Wait for /rest/ to answer and the start level to reach min_level.

    While userdata/cache-refresh-lock exists a throwaway server is priming
    the OSGi cache, so its REST answers do not count. Start levels (runtime/services.cfg) depend on add-ons and Things: 30
    waits for script engines such as jsscripting to install, 80 for every
    Thing to have an initialised handler. So by default only REST is awaited.
    """
    http = Http(inst.url, timeout=5)
    deadline = time.monotonic() + timeout
    root = None
    while time.monotonic() < deadline:
        if not group_members(pgid):
            raise SandboxError(f"{inst.name} exited during startup; see {inst.console_log}\n" + tail_text(inst.console_log, 20))
        if (inst.userdata / "cache-refresh-lock").exists():
            time.sleep(1)
            continue
        status, body = http.try_get_json("/rest/")
        if status == 200 and isinstance(body, dict) and body.get("runtimeInfo"):
            root = body
            break
        time.sleep(1)
    if root is None:
        raise SandboxError(f"{inst.name}: REST API did not answer within {timeout:.0f}s")
    while time.monotonic() < deadline:
        level = start_level(inst)
        if level is None or level >= min_level:
            return root
        if not group_members(pgid):
            raise SandboxError(f"{inst.name} exited during startup; see {inst.console_log}")
        time.sleep(1)
    raise SandboxError(
        f"{inst.name}: start level {min_level} not reached within {timeout:.0f}s (at {start_level(inst)})"
    )


def start_level(inst: Instance) -> int | None:
    """openHAB's current start level, which its start script mirrors into userdata."""
    try:
        return int((inst.userdata / "openhab-start-level").read_text().strip())
    except (OSError, ValueError):
        return None


def stop_instance(inst: Instance, timeout: float = STOP_TIMEOUT, log=print) -> str | None:
    """SIGTERM the process group (Karaf stops gracefully), then SIGKILL after timeout.

    Returns None if nothing was running, else "stopped" or "killed".
    """
    pgid = running_pgid(inst)
    if not pgid:
        inst.pid_file.unlink(missing_ok=True)
        return None
    lock = inst.userdata / "cache-refresh-lock"
    if lock.exists():
        # The cache-priming phase traps SIGTERM and carries on; a half-built
        # cache is discarded so the next start primes it again.
        _kill_group(pgid)
        lock.unlink(missing_ok=True)
        shutil.rmtree(inst.userdata / "cache", ignore_errors=True)
        inst.pid_file.unlink(missing_ok=True)
        return "killed"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and group_members(pgid):
        time.sleep(0.5)
    outcome = "stopped"
    if group_members(pgid):
        log(f"{inst.name}: still running after {timeout:.0f}s, sending SIGKILL")
        _kill_group(pgid)
        outcome = "killed"
    inst.pid_file.unlink(missing_ok=True)
    return outcome


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    end = time.monotonic() + 10
    while time.monotonic() < end and group_members(pgid):
        time.sleep(0.2)


def tail_text(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max(8192, lines * 400))
            fh.seek(size - block)
            data = fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


# --------------------------------------------------------------------------
# Karaf console client and API token


def karaf_command(rt: Runtime, inst: Instance, command: str, retries: int = 30) -> str:
    env = instance_env(rt, inst)
    # The client keeps known_hosts below user.home; keep it beside this userdata
    # so a fresh userdata (new host key) never collides with ~/.sshkaraf.
    client_home = inst.userdata / "karaf-client-home"
    client_home.mkdir(parents=True, exist_ok=True)
    env["JAVA_OPTS"] = f"-Duser.home={client_home}"
    argv = [
        str(rt.client), "-h", "127.0.0.1", "-a", str(inst.ssh_port),
        "-u", KARAF_USER, "-p", KARAF_PASSWORD, "-r", str(retries), "-d", "2", command,
    ]
    # The client closes the session without running the command when its stdin
    # is at EOF, so it gets a pipe that stays open until it exits.
    read_end, write_end = os.pipe()
    oh_cli.trace_run([("***" if before == "-p" else part) for before, part in zip(["", *argv], argv)])
    try:
        proc = subprocess.Popen(
            argv, env=env, stdin=read_end, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        os.close(read_end)
        read_end = -1
        try:
            output, _ = proc.communicate(timeout=retries * 3 + 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise SandboxError(f"karaf client timed out running '{' '.join(command.split()[:2])}'")
    finally:
        if read_end >= 0:
            os.close(read_end)
        os.close(write_end)
    output = re.sub(r"^.*AcceptAllServerKeyVerifier.*\n?", "", output, flags=re.MULTILINE)
    if proc.returncode != 0:
        verb = " ".join(command.split()[:2])
        raise SandboxError(f"karaf client '{verb}' failed ({proc.returncode}): {redact(output).strip()[-800:]}")
    return output


TOKEN_RE = re.compile(r"oh\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    return TOKEN_RE.sub("oh.<redacted>", text)


def parse_token(output: str) -> str:
    match = TOKEN_RE.search(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output))
    if not match:
        raise SandboxError("no API token in the console output: " + redact(output).strip()[-400:])
    return match.group(0)


def token_works(inst: Instance, token: str) -> bool:
    status, _ = Http(inst.url, token=token).try_get_json("/rest/addons?serviceId=karaf")
    return status == 200


def provision_token(rt: Runtime, inst: Instance, log=print) -> Path:
    """Ensure an administrator and a working API token for inst; return the token file.

    Uses the Karaf console's `openhab:users` commands over the local SSH
    console. The admin password and token live only in inst.state, mode 0600.
    """
    # Users and tokens live in jsondb, which is loaded by start level 20; before
    # that a stored token reads as unknown and adding a user fails.
    wait_until = time.monotonic() + 120
    while (start_level(inst) or 0) < 20 and time.monotonic() < wait_until:
        time.sleep(1)
    if inst.token_file.exists():
        token = read_token(inst.token_file)
        if token and token_works(inst, token):
            return inst.token_file
    users = karaf_command(rt, inst, "openhab:users list")
    if not re.search(rf"^{re.escape(ADMIN_USER)}\b", users, re.MULTILINE):
        password = secrets.token_urlsafe(18)
        # Right after REST comes up the user registry may still lack its
        # storage ("ManagedProvider is not available"); the console then
        # prints nothing useful and still exits 0, so retry until it confirms.
        deadline = time.monotonic() + 120
        while "User created" not in karaf_command(rt, inst, f"openhab:users add {ADMIN_USER} {password} administrator"):
            if time.monotonic() > deadline:
                raise SandboxError("openhab:users add did not confirm within 120s; see openhab.log")
            time.sleep(3)
        write_secret(inst.password_file, password)
        log(f"user    created openHAB administrator '{ADMIN_USER}' (password in {inst.password_file})")
    else:
        tokens = karaf_command(rt, inst, "openhab:users listApiTokens")
        if re.search(rf"\bname={re.escape(TOKEN_NAME)}\b", tokens):
            karaf_command(rt, inst, f"openhab:users rmApiToken {ADMIN_USER} {TOKEN_NAME}")
    output = karaf_command(rt, inst, f"openhab:users addApiToken {ADMIN_USER} {TOKEN_NAME} {TOKEN_SCOPE}")
    token = parse_token(output)
    write_secret(inst.token_file, token)
    deadline = time.monotonic() + 20
    while not token_works(inst, token):
        if time.monotonic() > deadline:
            raise SandboxError("the new API token is not accepted by /rest/addons")
        time.sleep(1)
    log(f"token   {inst.token_file} (mode 0600)")
    return inst.token_file


STATES_LEVEL = 30


def wait_addons(http: Http, wanted: set[str], deadline: float) -> set[str]:
    """Poll until every wanted add-on uid is installed or the deadline passes; return the missing."""
    missing = set(wanted)
    while missing:
        _, addons = http.try_get_json("/rest/addons?serviceId=all")
        missing = set(wanted) - installed_addon_uids(addons or [])
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(3)
    return missing


STUCK_DETAILS = ("HANDLER_MISSING_ERROR", "NOT_YET_READY")


def first_boot_stuck(level: int | None, things: list[dict]) -> list[str]:
    """Reasons a first boot has not come up although every add-on reports installed."""
    reasons = []
    if (level or 0) < STATES_LEVEL:
        reasons.append(f"start level {level} < {STATES_LEVEL}")
    stuck = [t.get("UID", "?") for t in things
             if t.get("statusInfo", {}).get("status") == "UNINITIALIZED"
             and t.get("statusInfo", {}).get("statusDetail") in STUCK_DETAILS]
    if stuck:
        reasons.append(f"{len(stuck)} Thing(s) without a handler")
    return reasons


def unstick_first_boot(rt: Runtime, inst: Instance, pgid: int, http: Http, deadline: float,
                       grace: float = 60, log=print) -> tuple[int, bool]:
    """Restart once if a first boot stays stuck after its add-ons installed.

    On a first boot (empty userdata) openHAB 5.2.3 can leave add-ons it
    installs from addons.cfg in OSGi state Resolved instead of Active, even
    though /rest/addons reports them installed. Seen here with jsscripting
    (start level then stays at 20, since 30 waits for script engines, and
    every Thing is UNINITIALIZED/NOT_YET_READY) and with bindings (Things
    UNINITIALIZED/HANDLER_MISSING_ERROR). One restart on the same userdata
    starts them. Returns (process group, restarted).
    """
    end = min(deadline, time.monotonic() + grace)
    reasons = ["not checked"]
    while time.monotonic() < end:
        code, things = http.try_get_json("/rest/things?summary=true")
        reasons = first_boot_stuck(start_level(inst), things or []) if code == 200 else ["/rest/things unreadable"]
        if not reasons:
            return pgid, False
        time.sleep(3)
    log(f"restart first boot stuck {grace:.0f}s after the add-on install ({'; '.join(reasons)}); restarting once")
    stop_instance(inst, log=log)
    pgid = start_instance(rt, inst, log=log)
    wait_rest(inst, pgid, max(1, deadline - time.monotonic()), log=log, min_level=STATES_LEVEL)
    log(f"restart done: start level {start_level(inst)}")
    return pgid, True


# --------------------------------------------------------------------------
# warmed boot-check userdata


TEMPLATE_FORMAT = 1
COMPLETE_LEVEL = 100
# What a finished boot leaves in userdata that must not leak into the next
# run: its log (parsed for problems), Inbox results and runtime markers.
TEMPLATE_SCRUB = ("logs", "jsondb/backup", "jsondb/org.openhab.core.config.discovery.DiscoveryResult.json",
                  "tmp/karaf.pid", "tmp/port")


def template_key(rt: Runtime, addons_cfg: str) -> str:
    """Name a warmed userdata after everything that shapes it."""
    material = json.dumps({
        "format": TEMPLATE_FORMAT,
        "openhab": rt.openhab_version,
        "java": rt.java_home.name,
        "addons.cfg": addons_cfg,
        "features": FEATURES_PROCESSING,
    }, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def scrub_template(userdata: Path) -> None:
    for rel in TEMPLATE_SCRUB:
        path = userdata / rel
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    (userdata / "logs").mkdir(exist_ok=True)
    (userdata / "openhab-start-level").write_text("0\n")


def token_persisted(userdata: Path) -> bool:
    """True once jsondb holds the admin's API token (openHAB writes jsondb with a delay)."""
    try:
        users = json.loads((userdata / "jsondb" / "users.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    user = (users.get(ADMIN_USER) or {}).get("value") or {}
    return any(t.get("name") == TOKEN_NAME for t in user.get("apiTokens") or [])


def warm_template(rt: Runtime, inst: Instance, deadline: float, log=print) -> tuple[Path, bool]:
    """Userdata with the OSGi cache built, conf's add-ons installed and an API token.

    Built once per template_key by booting on a conf that holds only
    addons.cfg, then killed (see SAVE_DELAY_MS). It is only ever
    restored at inst.userdata, the path it was built at. Returns
    (template directory, whether it was built now).
    """
    cfg = inst.conf / "services" / "addons.cfg"
    addons_text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    templates = inst.state / "template"
    tdir = templates / template_key(rt, addons_text)
    if (tdir / "userdata" / "cache" / "org.eclipse.osgi").is_dir() and (tdir / inst.token_file.name).exists():
        return tdir, False
    started = time.monotonic()
    wipe_dir(templates, root=inst.state)
    conf = inst.state / "template-conf"
    wipe_dir(conf, root=inst.state)
    (conf / "services").mkdir(parents=True)
    (conf / "services" / "addons.cfg").write_text(addons_text, encoding="utf-8")
    build = dataclasses.replace(inst, conf=conf.resolve())
    wipe_dir(build.userdata)
    for path in (build.token_file, build.password_file):
        path.unlink(missing_ok=True)
    log(f"template building warmed userdata {tdir.name} (once per add-on list and runtime pin)")
    pgid = start_instance(rt, build, log=log)
    try:
        wait_rest(build, pgid, max(1, deadline - time.monotonic()), log=log)
        http = Http(build.url, token=read_token(provision_token(rt, build, log=log)), timeout=30)
        missing = wait_addons(http, set(addon_uids(parse_addons_cfg(addons_text))), deadline)
        if missing:
            raise SandboxError("template: add-ons not installed: " + ", ".join(sorted(missing)))
        pgid, _ = unstick_first_boot(rt, build, pgid, http, deadline, log=log)
        while (start_level(build) or 0) < COMPLETE_LEVEL or not token_persisted(build.userdata):
            if time.monotonic() >= deadline:
                raise SandboxError(f"template: start level {start_level(build)} (want {COMPLETE_LEVEL}), "
                                   f"API token saved: {token_persisted(build.userdata)}")
            time.sleep(1)
        time.sleep(3 * SAVE_DELAY_MS / 1000)
    except BaseException:
        stop_instance(build, timeout=5, log=log)
        raise
    _kill_group(pgid)
    if group_members(pgid):
        raise SandboxError(f"template: openHAB (process group {pgid}) survived SIGKILL")
    build.pid_file.unlink(missing_ok=True)
    scrub_template(build.userdata)
    tdir.mkdir(parents=True)
    os.rename(build.userdata, tdir / "userdata")
    for path in (build.token_file, build.password_file):
        if path.exists():
            os.rename(path, tdir / path.name)
    wipe_dir(conf, root=inst.state)
    log(f"template built in {time.monotonic() - started:.0f}s")
    return tdir, True


def restore_template(tdir: Path, inst: Instance) -> None:
    """Replace inst's userdata, API token and admin password with copies from a warmed template."""
    wipe_dir(inst.userdata)
    shutil.copytree(tdir / "userdata", inst.userdata, symlinks=True)
    for dest in (inst.token_file, inst.password_file):
        src = tdir / dest.name
        dest.unlink(missing_ok=True)
        if src.exists():
            shutil.copy2(src, dest)


@dataclasses.dataclass
class Boot:
    pgid: int
    token: str | None
    #: Requested add-ons still not installed at the deadline.
    missing: set[str]
    restarted: bool
    timings: dict[str, float]


def boot(rt: Runtime, inst: Instance, requested: dict[str, list[str]], started: float, deadline: float,
         log=print, *, before_start=None) -> Boot:
    """Start inst from its warmed template and wait for its REST API and add-ons.

    Timings are seconds since `started`. A template that did not come up
    clean is discarded, so the next boot rebuilds it. before_start(inst),
    when given, prepares instance-specific state after the template is
    restored and before any rule loads it."""
    timings: dict[str, float] = {}
    template, built = warm_template(rt, inst, deadline, log=log)
    if built:
        timings["template"] = time.monotonic() - started
    restore_template(template, inst)
    if before_start is not None:
        before_start(inst)
    pgid = start_instance(rt, inst, log=log)
    log(f"boot    openHAB {rt.openhab_version} on {inst.conf} at {inst.url}")
    wait_rest(inst, pgid, max(1, deadline - time.monotonic()), log=log)
    timings["REST up"] = time.monotonic() - started
    token = read_token(provision_token(rt, inst, log=log))
    http = Http(inst.url, token=token, timeout=30)

    wanted = set(addon_uids(requested))
    missing = wait_addons(http, wanted, deadline)
    timings["add-ons"] = time.monotonic() - started
    log(f"addons  {len(wanted) - len(missing)}/{len(wanted)} requested add-ons installed "
        f"at +{timings['add-ons']:.0f}s")
    restarted = False
    if not missing:
        pgid, restarted = unstick_first_boot(rt, inst, pgid, http, deadline, log=log)
    if restarted:
        timings["restart"] = time.monotonic() - started
    if not built and (missing or restarted):
        wipe_dir(inst.state / "template", root=inst.state)
        log("template discarded: a boot from it did not come up clean; the next run rebuilds it")
    return Boot(pgid, token, missing, restarted, timings)


def die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)
