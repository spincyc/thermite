"""thermite test boot: boot a test runtime on a conf directory and report
whether it loads cleanly."""

from __future__ import annotations

import http.client
import json
import re
import sys
import time
import urllib.error

import oh_cli
import oh_deploy as deploy
import oh_render as render
import oh_sandbox as lib
from oh_metrics import dashboards

SUMMARY = "boot conf/ on a test runtime and report how it loads"

DESCRIPTION = """\
Boot a test runtime (the pinned distribution, see thermite sandbox) on
CONF, on 127.0.0.1:13619 (HTTPS 13620, Karaf console 13621) so it can run
beside the sandbox. It boots on var/boot-check/conf, a copy of CONF that
commands nothing (oh_render.read_only) and reaches no device
(oh_render.unaddressed): every address in it is the limited broadcast
address, which the kernel refuses before a packet leaves, every Sonos UDN is
one no player has, and UPnP has no network. Wait for the add-ons in
CONF/services/addons.cfg and for Things to settle (none waiting for a
handler, and no status change for --settle seconds, or 2s once every Thing
is ONLINE), then report the runtime version, requested vs installed add-ons,
Things by status/statusDetail, the Item count, model problems (DSL and YAML
files, scripts that fail to load) and other ERROR records from openhab.log,
and that /rest/metrics/prometheus (the Metrics add-on, which Prometheus
scrapes) answers with every runtime series the dashboards chart.
If Things still wait for a handler and neither they nor the start level
change for --stall seconds, stop waiting and fail on them.

The check proves conf/ loads and every Thing gets a handler without a
configuration error. With no device answering, a Thing OFFLINE,
INITIALIZING or UNKNOWN only warns. Device reachability is thermite
health's job.

Each run starts from a copy of a warmed userdata in var/boot-check/template/
(OSGi cache built, add-ons installed, admin user and API token created), so
runtime starts without downloading anything. The template is built on the
first run and again whenever addons.cfg or a runtime pin changes, or with
--rebuild; that run takes about a minute longer.

A clean run records the digest of CONF's files in var/boot-check/passed/;
thermite deploy installs only a conf/ with such a record.

The instance is stopped at the end unless --keep is given. Its userdata is a
disposable copy, so it is killed if it has not stopped within 3s.
"""

EXAMPLES = (
    ("thermite test boot", "boot conf/ on a test runtime and report how it loads"),
    ("thermite test boot --json --keep", "the report as JSON; leave the instance running"),
)

NOTES = """\
exit status:
  0  clean
  1  model problems in openhab.log, requested add-ons not installed, or a
     Thing UNINITIALIZED or in HANDLER_CONFIGURATION_PENDING /
     CONFIGURATION_ERROR
  2  the instance could not be booted or queried
"""

# The userdata is a copy of the template, so killing loses nothing; a graceful
# stop has taken minutes (see lib.SAVE_DELAY_MS).
STOP_TIMEOUT = 3
#: What Prometheus scrapes (oh_metrics prometheus.yml's openhab job).
METRICS_PATH = "/rest/metrics/prometheus"
SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{| )")
THINGS_PATH = "/rest/things?summary=true"
REPORT_THINGS_PATH = "/rest/things?summary=false"


class ReportError(lib.SandboxError):
    """A fixed-route query diagnostic; never include an upstream body or log tail."""


class ReportUnavailable(ReportError):
    """A temporary REST registration or connection outage within the boot budget."""


class ReportHttp(lib.Http):
    """Read a fresh report under one absolute deadline, including every request."""

    def __init__(self, url, token, deadline):
        super().__init__(url, token=token, timeout=30, deadline=deadline)
        self.last_unavailable = None

    def remaining(self, path):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            detail = f" (last outage: {self.last_unavailable})" if self.last_unavailable else ""
            raise ReportError(f"boot-check: startup deadline expired querying {path}{detail}")
        return remaining

    def request(self, method, path, body=None, headers=None):
        self.timeout = min(30, self.remaining(path))
        try:
            status, data = self._request(method, path, body=body, headers=headers)
        except urllib.error.HTTPError as exc:
            status, data = exc.code, b""
            exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            status, data = 0, b""
        self.remaining(path)
        if status == 0 or status == 404 or 500 <= status <= 599:
            issue = f"{path} is unavailable (HTTP {status})" if status else f"{path} has a connection failure"
            self.last_unavailable = issue
            raise ReportUnavailable(f"boot-check: {issue}")
        if status != 200:
            raise ReportError(f"boot-check: {path} answered HTTP {status}")
        return status, data

    def get_json(self, path):
        try:
            value = super().get_json(path)
        except (ValueError, UnicodeError, RecursionError):
            raise ReportError(f"boot-check: {path} returned malformed JSON") from None
        valid = False
        if path == "/rest/":
            info = value.get("runtimeInfo") if isinstance(value, dict) else None
            valid = isinstance(info, dict) and isinstance(info.get("version"), str) and bool(info["version"])
        elif path == "/rest/systeminfo":
            info = value.get("systemInfo") if isinstance(value, dict) else None
            valid = isinstance(info, dict) and isinstance(info.get("javaVendorVersion"), str) and bool(info["javaVendorVersion"])
        elif path == "/rest/addons?serviceId=all":
            valid = isinstance(value, list) and all(isinstance(row, dict) and type(row.get("installed")) is bool
                and (isinstance(row.get("uid"), str) and bool(row["uid"])
                     or all(isinstance(row.get(key), str) and bool(row[key]) for key in ("type", "id")))
                for row in value)
        elif path in (THINGS_PATH, REPORT_THINGS_PATH):
            valid = isinstance(value, list) and all(isinstance(row, dict) and isinstance(row.get("UID"), str) and bool(row["UID"])
                and isinstance(row.get("statusInfo"), dict) and isinstance(row["statusInfo"].get("status"), str) and bool(row["statusInfo"]["status"])
                and (row["statusInfo"].get("statusDetail") is None or isinstance(row["statusInfo"]["statusDetail"], str))
                for row in value)
        elif path == "/rest/items?recursive=false&fields=name,type":
            valid = isinstance(value, list) and all(isinstance(row, dict)
                and all(isinstance(row.get(key), str) and bool(row[key]) for key in ("name", "type")) for row in value)
        if not valid:
            raise ReportError(f"boot-check: {path} returned an invalid JSON shape")
        self.remaining(path)
        return value


def series_names(text: str) -> list[str]:
    """The sample names of a Prometheus text exposition."""
    return sorted({found[1] for line in text.splitlines() if (found := SAMPLE.match(line))})


def metrics(http: lib.Http) -> dict:
    """Whether the Metrics add-on answers with the boot instance's token,
    and which series the dashboards use (dashboards.OPENHAB_METRICS) it
    lacks: `missing` ones runtime always has, `not_yet` ones that appear
    only after a rule ran or a garbage collection (dashboards.ON_ACTIVITY)."""
    try:
        status, data = http.request("GET", METRICS_PATH, headers={"Accept": "text/plain"})
    except urllib.error.HTTPError as exc:
        exc.close()
        status, data = exc.code, b""
    except OSError:
        status, data = 0, b""
    names = series_names(data.decode("utf-8", "replace")) if status == 200 else []
    later = {name for key, name in dashboards.OPENHAB_METRICS.items() if key in dashboards.ON_ACTIVITY}
    wanted = sorted(set(dashboards.OPENHAB_METRICS.values()))
    absent = [name for name in wanted if name not in names] if status == 200 else wanted
    return {"path": METRICS_PATH, "http": status, "series": len(names),
            "missing": [name for name in absent if name not in later],
            "not_yet": [name for name in absent if name in later]}


def collect(inst: lib.Instance, http: lib.Http, requested: dict) -> dict:
    root = http.get_json("/rest/")
    sysinfo = http.get_json("/rest/systeminfo")
    addons = http.get_json("/rest/addons?serviceId=all")
    # Classify optional HEOS login diagnostics from the full DTO. Settling reads
    # remain summaries; the report retains no configuration or account fields.
    things = http.get_json(REPORT_THINGS_PATH)
    items = http.get_json("/rest/items?recursive=false&fields=name,type")
    installed = sorted(lib.installed_addon_uids(addons))
    wanted = lib.addon_uids(requested)
    text = inst.openhab_log.read_text(encoding="utf-8", errors="replace") if inst.openhab_log.exists() else ""
    problems = lib.log_problems(lib.parse_log(text))
    return {
        "openhab_version": root["runtimeInfo"]["version"],
        "java": sysinfo["systemInfo"]["javaVendorVersion"],
        "conf": str(inst.conf),
        "addons": {
            "requested": wanted,
            "installed": installed,
            "missing": [uid for uid in wanted if uid not in installed],
            "unrequested": [uid for uid in installed if uid not in wanted],
        },
        "things": lib.summarize_things(things),
        "items": {"count": len(items), "by_type": _count(i.get("type", "?") for i in items)},
        "metrics": metrics(http),
        "log": {
            "file": str(inst.openhab_log),
            "model_problems": [_record(r) for r in problems["model"]],
            "errors": [_record(r) for r in problems["errors"]],
        },
    }


def _count(values) -> dict:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _record(record: lib.LogRecord) -> dict:
    return {"line": record.lineno, "ts": record.ts, "level": record.level, "logger": record.logger,
            "message": record.message}


def verdict(report: dict) -> list[str]:
    reasons = []
    if report["log"]["model_problems"]:
        reasons.append(f"{len(report['log']['model_problems'])} model problem(s) in openhab.log")
    if report["addons"]["missing"]:
        reasons.append("add-ons not installed: " + ", ".join(report["addons"]["missing"]))
    if report["things"]["failed"]:
        reasons.append(f"{len(report['things']['failed'])} Thing(s) failed")
    found = report.get("metrics")
    if found and found["http"] != 200:
        reasons.append(f"{found['path']} answered HTTP {found['http'] or 'connection failed'} (the Metrics add-on)")
    elif found and found["missing"]:
        reasons.append(f"{found['path']} lacks series the dashboards use: {', '.join(found['missing'])}")
    return reasons


def print_human(report: dict) -> None:
    t = report["timings"]
    print(f"runtime {report['openhab_version']} on {report['java']}  conf={report['conf']}")
    level = report["start_level"]
    print(f"start level {level}" + ("" if level == 100 else
          "  (below 100: an add-on or a Thing handler never finished initialising)"))
    print("timings: " + ", ".join(f"{k} {v:.0f}s" for k, v in t.items()))
    if report["restarted_after_first_boot"]:
        print("restarted once: the first boot stayed stuck after its add-ons installed (see thermite sandbox --help)")
    a = report["addons"]
    print(f"\nadd-ons requested ({len(a['requested'])}): {', '.join(a['requested']) or '-'}")
    print(f"add-ons installed ({len(a['installed'])}): {', '.join(a['installed']) or '-'}")
    if a["missing"]:
        print(f"MISSING: {', '.join(a['missing'])}")
    if a["unrequested"]:
        print(f"installed but not in addons.cfg: {', '.join(a['unrequested'])}")
    th = report["things"]
    note = {"settled": "", "timeout": "  (still changing at the timeout)",
            "stalled": f"  (stopped waiting: nothing changed for {report['stall_seconds']:.0f}s)"}
    print(f"\nThings: {th['count']}" + note[report["things_outcome"]])
    for key, count in th["by_status"].items():
        print(f"  {count:4d}  {key}")
    for row in th["things"]:
        if row["verdict"] == "ok":
            continue
        mark = "FAIL" if row["verdict"] == "fail" else "warn"
        desc = f"  {row['description']}" if row.get("description") else ""
        print(f"  {mark}  {row['uid']}  {row['status']}/{row['detail']}{desc}")
    it = report["items"]
    print(f"\nItems: {it['count']}  " + ", ".join(f"{k} {v}" for k, v in it["by_type"].items()))
    found = report["metrics"]
    if found["http"] == 200:
        print(f"metrics: {found['path']} answers, {found['series']} series"
              + (f"; MISSING for the dashboards: {', '.join(found['missing'])}" if found["missing"] else
                 "; every series the dashboards use is there" if not found.get("not_yet") else
                 "; every series the dashboards always need is there"))
        if found.get("not_yet"):
            print(f"  warn  not there yet, until a rule runs or a garbage collection: {', '.join(found['not_yet'])}")
    else:
        print(f"metrics: {found['path']} answered HTTP {found['http'] or 'connection failed'}")
    lg = report["log"]
    print(f"\nmodel problems ({len(lg['model_problems'])}):")
    for rec in lg["model_problems"]:
        message = rec["message"].rstrip().replace("\n", "\n      ")
        print(f"  openhab.log:{rec['line']} [{rec['level']}] [{rec['logger']}] {message}")
    print(f"other ERROR records ({len(lg['errors'])}):")
    for rec in lg["errors"]:
        first = rec["message"].split("\n", 1)[0]
        print(f"  openhab.log:{rec['line']} [{rec['logger']}] {first}")
    print()
    if report["failures"]:
        print("RESULT: FAIL - " + "; ".join(report["failures"]))
    else:
        print("RESULT: ok")
    if report.get("kept"):
        print(f"kept running at {report['url']} (token {report['token_file']}); stop with: "
              f"kill -TERM -- -$(cat {report['pid_file']})")


def run_check(args, rt: lib.Runtime, inst: lib.Instance, log) -> dict:
    if lib.running_pgid(inst):
        raise lib.SandboxError(f"a kept test instance is still running (pid file {inst.pid_file}); "
                               f"stop it with kill -TERM -- -$(cat {inst.pid_file})")
    requested = lib.read_requested_addons(inst.conf)
    inst.console_log.unlink(missing_ok=True)
    if args.rebuild:
        lib.wipe_dir(inst.state / "template", root=inst.state)

    started = time.monotonic()
    deadline = started + args.timeout
    keep = False
    try:
        booted = lib.boot(rt, inst, requested, started, deadline, log=log)
        timings, restarted = booted.timings, booted.restarted
        http = ReportHttp(inst.url, booted.token, deadline)

        settler = lib.Settler(args.settle, stall=args.stall)
        outcome = None
        while True:
            http.remaining(THINGS_PATH)
            try:
                if outcome != "stalled":
                    things = http.get_json(THINGS_PATH)
                    now = time.monotonic()
                    if settler.observe(things, now, lib.start_level(inst)):
                        outcome = "settled"
                    elif settler.stalled(now):
                        outcome = "stalled"
                    if outcome:
                        timings[f"things {outcome}"] = time.monotonic() - started
                if outcome:
                    report = collect(inst, http, requested)
                    http.remaining(METRICS_PATH)
                    break
            except ReportUnavailable:
                # An attempt is all-or-nothing, including optional metadata,
                # add-ons and metrics. Never reuse fields from a failed read.
                if outcome != "stalled":
                    timings.pop("things settled", None)
                    outcome = None
                    settler = lib.Settler(args.settle, stall=args.stall)
            time.sleep(min(1, http.remaining(THINGS_PATH)))
        report["start_level"] = lib.start_level(inst)
        report["restarted_after_first_boot"] = restarted
        report["things_outcome"] = outcome
        report["stall_seconds"] = args.stall
        report["timings"] = timings
        report["failures"] = verdict(report)
        if outcome == "stalled":
            report["failures"].append(f"Things stalled for {args.stall:.0f}s")
        report["url"] = inst.url
        report["token_file"] = str(inst.token_file)
        report["pid_file"] = str(inst.pid_file)
        keep = args.keep
        report["kept"] = keep
        return report
    except ReportError:
        raise
    except lib.SandboxError as exc:
        if inst.openhab_log.is_file():
            tail = lib.tail_text(inst.openhab_log, 30)
            raise lib.SandboxError(f"{exc}\n--- last lines of {inst.openhab_log}\n{tail}") from exc
        raise
    finally:
        if not keep:
            stop_started = time.monotonic()
            if lib.stop_instance(inst, timeout=STOP_TIMEOUT, log=log):
                log(f"stopped the test instance in {time.monotonic() - stop_started:.0f}s")


def add_arguments(parser) -> None:
    oh_cli.add_conf(parser)
    from pathlib import Path
    import argparse
    parser.add_argument("--versions", type=Path, help=argparse.SUPPRESS)
    oh_cli.add_timeout(parser, 300, "the whole check")
    parser.add_argument("--settle", type=float, default=5, metavar="SECONDS",
                        help="seconds Things must stay unchanged (default: 5)")
    parser.add_argument("--stall", type=float, default=30, metavar="SECONDS",
                        help="stop waiting once Things without a handler and the start level stay unchanged "
                             "this long (default: 30)")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the warmed userdata template first")
    oh_cli.add_json(parser)
    parser.add_argument("--keep", action="store_true", help="leave the instance running afterwards")


def run(args) -> int:
    log = (lambda msg: print(msg, file=sys.stderr)) if args.json else print

    try:
        versions_file = getattr(args, "versions", None)
        versions = lib.load_versions(versions_file) if versions_file is not None else lib.load_versions()
        conf = lib.VAR / "boot-check" / "conf"
        inst = lib.boot_check_instance(conf)
        with lib.InstanceLock(inst):
            if lib.running_pgid(inst):
                raise lib.SandboxError(f"a kept test instance is still running (pid file {inst.pid_file}); "
                                       f"stop it with kill -TERM -- -$(cat {inst.pid_file})")
            digest = deploy.tree_digest(deploy.read_tree(args.conf))
            lib.mirror_conf(args.conf, conf, lambda files: render.unaddressed(render.read_only(files)))
            rt = lib.fetch(versions, log=log)
            report = run_check(args, rt, inst, log)
    except (lib.SandboxError, deploy.DeployError) as exc:
        lib.die(str(exc))
    if not report["failures"]:
        deploy.record_boot_check(lib.VAR, digest)
        report["conf_digest"] = digest

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print_human(report)
        if not report["failures"]:
            print(f"recorded the pass for this conf/ ({digest[:12]}): thermite deploy may install it")
    return 1 if report["failures"] else 0
