"""thermite test boot: boot a test runtime on a conf directory and report
whether it loads cleanly."""

from __future__ import annotations

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
CONF, on 127.0.0.1:18081 (HTTPS 18444, Karaf console 18102) so it can run
beside the sandbox. It boots on var/boot-check/conf, a copy of CONF in which
every HTTP binding channel is READONLY (oh_render.read_only), so it polls the
real matrices but cannot switch them. Wait for the add-ons in
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
configuration error. A Thing still INITIALIZING/UNKNOWN waits for its device
and only warns; with --require-online the check also waits for devices and
fails on any Thing not ONLINE. Device reachability is thermite health's job:
the sandbox's web server is bound to 127.0.0.1, so UPnP event callbacks from
speakers cannot reach it and they may only report at the binding's
60s poll.

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
    ("thermite test boot --require-online", "also wait for every device, and fail on any Thing not ONLINE"),
    ("thermite test boot --json --keep", "the report as JSON; leave the instance running"),
)

NOTES = """\
exit status:
  0  clean
  1  model problems in openhab.log, requested add-ons not installed, or a
     Thing UNINITIALIZED or in HANDLER_CONFIGURATION_PENDING /
     CONFIGURATION_ERROR (with --require-online: any Thing not ONLINE)
  2  the instance could not be booted or queried
"""

# The userdata is a copy of the template, so killing loses nothing; a graceful
# stop has taken minutes (see lib.SAVE_DELAY_MS).
STOP_TIMEOUT = 3
#: What Prometheus scrapes (oh_metrics prometheus.yml's openhab job).
METRICS_PATH = "/rest/metrics/prometheus"
SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{| )")


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


def collect(inst: lib.Instance, http: lib.Http, requested: dict, require_online: bool) -> dict:
    root = http.get_json("/rest/")
    _, sysinfo = http.try_get_json("/rest/systeminfo")
    _, addons = http.try_get_json("/rest/addons?serviceId=all")
    things = http.get_json("/rest/things?summary=true") or []
    items = http.get_json("/rest/items?recursive=false&fields=name,type") or []
    installed = sorted(lib.installed_addon_uids(addons or []))
    wanted = lib.addon_uids(requested)
    text = inst.openhab_log.read_text(encoding="utf-8", errors="replace") if inst.openhab_log.exists() else ""
    problems = lib.log_problems(lib.parse_log(text))
    return {
        "openhab_version": (root or {}).get("runtimeInfo", {}).get("version"),
        "java": (sysinfo or {}).get("systemInfo", {}).get("javaVendorVersion"),
        "conf": str(inst.conf),
        "addons": {
            "requested": wanted,
            "installed": installed,
            "missing": [uid for uid in wanted if uid not in installed],
            "unrequested": [uid for uid in installed if uid not in wanted],
        },
        "things": lib.summarize_things(things, require_online=require_online),
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
        http = lib.Http(inst.url, token=booted.token, timeout=30)

        settler = lib.Settler(args.settle, stall=args.stall, devices=args.require_online)
        outcome = "timeout"
        while time.monotonic() < deadline:
            now = time.monotonic()
            code, things = http.try_get_json("/rest/things?summary=true")
            if code == 200:
                if settler.observe(things or [], now, lib.start_level(inst)):
                    outcome = "settled"
                    break
                if settler.stalled(now):
                    outcome = "stalled"
                    break
            time.sleep(1)
        timings[f"things {outcome}"] = time.monotonic() - started

        report = collect(inst, http, requested, args.require_online)
        report["start_level"] = lib.start_level(inst)
        report["restarted_after_first_boot"] = restarted
        report["things_outcome"] = outcome
        report["stall_seconds"] = args.stall
        report["timings"] = timings
        report["failures"] = verdict(report)
        report["url"] = inst.url
        report["token_file"] = str(inst.token_file)
        report["pid_file"] = str(inst.pid_file)
        keep = args.keep
        report["kept"] = keep
        return report
    except lib.SandboxError as exc:
        tail = lib.tail_text(inst.openhab_log, 30)
        raise lib.SandboxError(f"{exc}\n--- last lines of {inst.openhab_log}\n{tail}") from exc
    finally:
        if not keep:
            stop_started = time.monotonic()
            if lib.stop_instance(inst, timeout=STOP_TIMEOUT, log=log):
                log(f"stopped the test instance in {time.monotonic() - stop_started:.0f}s")


def add_arguments(parser) -> None:
    oh_cli.add_conf(parser)
    oh_cli.add_timeout(parser, 300, "the whole check")
    parser.add_argument("--settle", type=float, default=5, metavar="SECONDS",
                        help="seconds Things must stay unchanged (default: 5)")
    parser.add_argument("--stall", type=float, default=30, metavar="SECONDS",
                        help="stop waiting once Things without a handler and the start level stay unchanged "
                             "this long (default: 30)")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the warmed userdata template first")
    parser.add_argument("--require-online", action="store_true", help="fail on any Thing that is not ONLINE")
    oh_cli.add_json(parser)
    parser.add_argument("--keep", action="store_true", help="leave the instance running afterwards")


def run(args) -> int:
    log = (lambda msg: print(msg, file=sys.stderr)) if args.json else print

    try:
        versions = lib.load_versions()
        conf = lib.VAR / "boot-check" / "conf"
        inst = lib.boot_check_instance(conf)
        with lib.InstanceLock(inst):
            if lib.running_pgid(inst):
                raise lib.SandboxError(f"a kept test instance is still running (pid file {inst.pid_file}); "
                                       f"stop it with kill -TERM -- -$(cat {inst.pid_file})")
            digest = deploy.tree_digest(deploy.read_tree(args.conf))
            lib.mirror_conf(args.conf, conf, render.read_only)
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
