"""Run a thermite sandbox locally, as this user, from the pinned distribution zip."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import oh_deploy as deploy
import oh_render as render
import oh_sandbox as lib
import oh_cli
from oh_cli import AREA

DESCRIPTION = """\
Run a local runtime sandbox from the distribution zip and a Temurin JRE, both
pinned in versions.json and cached under build/sandbox/. No root, container
runtime or system Java is needed.

The sandbox binds HTTP to 127.0.0.1:18080 and HTTPS to 127.0.0.1:18443
(OPENHAB_HTTP_ADDRESS/OPENHAB_HTTP_PORT/OPENHAB_HTTPS_PORT, honoured by
runtime/bin/setenv), so the UI is not reachable from the LAN (but see
--live below). The Karaf
console listens on 127.0.0.1:18101. Bindings still open UPnP/mDNS sockets on
the LAN.

It runs on var/sandbox/conf, a copy of conf/ in which every HTTP binding
channel is READONLY, the receivers' Items are display-only and the
projector's rule sends nothing (oh_render.read_only): it reads the matrices
and the devices under control but cannot command them, and every other
read-write channel is guarded in conf/ itself.
`reload` refreshes the copy after `thermite generate`; the running sandbox
picks the change up.

`start --live` is a test instance for the house instead: its copy of conf/
is unchanged, so its Source selectors switch the real matrices and its
remote drives the devices under control (the Apple TVs through thermite
apple-tv serve; every other control stays guarded in conf/), and HTTP and HTTPS
listen on every interface, so phones on the LAN reach it on port 18080 of
this host. Anyone on the LAN can then do what the runtime's implicit 'user' role
allows, choosing sources and driving those devices included; administration
still needs the admin login. `reload` keeps the mode the sandbox was started in;
stop and start it to change modes. It refuses to start while production runtime runs
on this host (it answers on 127.0.0.1 at instance/host.yaml's http_port, or
openhab.service is up): two live instances would fight over the same
devices and pairings. It cannot see a production runtime on another host;
make sure none runs before starting one.

State lives in var/sandbox/: conf/, userdata/ (the runtime's userdata, initialised
from the distribution template when empty), openhab.pid, console.log,
api-token and admin-password.
"""

NOTES = """\
REST authentication:
  runtime 5 lets anonymous clients use 'user' endpoints (implicit user role)
  but needs an administrator for others, such as /rest/addons. After the REST
  API answers, `start` logs into the local Karaf console (runtime/bin/client
  as the distribution's console user openhab/habopen, 127.0.0.1:18101 only)
  and runs:
      openhab:users add admin <random password> administrator
      openhab:users addApiToken admin automation sandbox
  It writes the token to var/sandbox/api-token and the password to
  var/sandbox/admin-password, both mode 0600 and gitignored. A token that
  still works is reused on the next start. Send it as
      curl -H "Authorization: Bearer $(cat var/sandbox/api-token)" \\
           http://127.0.0.1:18080/rest/addons
  and log into the UI as 'admin' with the stored password. --fresh discards
  both together with userdata.

first boot:
  With empty userdata, start first builds the OSGi cache with a separate
  server that gets no add-ons and must exit completely (about 20s). The
  distribution's own priming in runtime/bin/karaf races the real server
  and can leave add-ons that Karaf lists as installed without their bundles
  (Things UNINITIALIZED/HANDLER_MISSING_ERROR on every later start; only
  --fresh recovers). start then waits for the add-ons in conf's
  services/addons.cfg to install. If the start level stays below 30 or
  Things stay UNINITIALIZED/HANDLER_MISSING_ERROR or NOT_YET_READY for 60s
  after that, start restarts runtime once on the same userdata.

stopping:
  Karaf shutdown hangs for minutes (Jetty stopping its selectors in
  WebClientFactoryImpl.deactivate, jUPnP unsubscribing from every device),
  so stop sends SIGKILL after --timeout. runtime runs with Equinox saving its
  bundle database one second after a change, so a kill loses no add-on
  installed more than a few seconds earlier. If a killed sandbox does not
  start again normally, use start --fresh.

pairing:
  An Android TV pairs with the sandbox through thermite pair DEVICE
  --sandbox; its pairing is kept in userdata/androidtv/, so a --fresh start
  pairs again.
"""

EXAMPLES = (
    ("thermite sandbox fetch", "the pinned JRE and distribution, once"),
    ("thermite sandbox start --fresh", "a read-only sandbox on an empty userdata"),
    ("thermite sandbox logs -f", "follow its openhab.log"),
    ("thermite sandbox reload", "after thermite generate: the running sandbox picks up conf/"),
    ("thermite sandbox stop", "stop it"),
)


def cmd_fetch(args) -> int:
    lib.fetch(lib.load_versions())
    return 0


SANDBOX_CONF = lib.VAR / "sandbox" / "conf"


def mirror(source: Path | None, live: bool) -> Path:
    return lib.mirror_conf(source or render.CONF, SANDBOX_CONF, dict if live else render.read_only)


def describe(mode: str) -> str:
    return ("live: the matrices, receivers, WiiM, projector, TVs, SHIELDs and Apple TVs are driven"
            if mode == lib.LIVE
            else "read-only: no device is commanded")


def refuse_beside_production() -> None:
    found = deploy.running_here(deploy.load(AREA)[1])
    if found:
        lib.die(f"{found}; a live sandbox beside it would drive the same devices. Test with thermite test "
                "(make openhab-test), then make openhab-deploy (docs/deploy.md)")


def cmd_start(args) -> int:
    if args.live:
        refuse_beside_production()
    versions = lib.load_versions()
    rt = lib.require_runtime(versions)
    inst = lib.sandbox_instance(SANDBOX_CONF, args.userdata, live=args.live)
    with lib.InstanceLock(inst):
        return _start(args, rt, inst)


def cmd_reload(args) -> int:
    mode = lib.read_mode(lib.sandbox_instance())
    conf = mirror(args.conf, mode == lib.LIVE)
    print(f"reloaded {conf} from {args.conf or render.CONF} ({describe(mode)})")
    return 0


def _start(args, rt, inst) -> int:
    if lib.running_pgid(inst):
        lib.die(f"the sandbox is already running ({inst.url}); stop it first, or reload")
    mode = lib.LIVE if args.live else lib.READ_ONLY
    mirror(args.conf, args.live)
    lib.write_mode(inst, mode)
    print(f"conf    {inst.conf} ({describe(mode)})")
    if args.fresh:
        if not lib.is_disposable(inst.userdata):
            lib.die(f"--fresh only empties userdata below {lib.VAR}; {inst.userdata} is not")
        lib.wipe_dir(inst.userdata)
        for path in (inst.token_file, inst.password_file):
            path.unlink(missing_ok=True)
        print(f"fresh   emptied {inst.userdata}")
    started = time.monotonic()
    deadline = started + args.timeout
    first_boot = not (inst.userdata / "etc").exists()
    pgid = lib.start_instance(rt, inst)
    print(f"start   runtime {rt.openhab_version} (pid {pgid}) conf={inst.conf} userdata={inst.userdata}")
    try:
        root = lib.wait_rest(inst, pgid, args.timeout)
        print(f"rest    {inst.url}/rest/ answers: runtime {root['runtimeInfo'].get('version')} "
              f"after {time.monotonic() - started:.0f}s")
        if not args.no_token:
            token = lib.read_token(lib.provision_token(rt, inst))
            wanted = set(lib.addon_uids(lib.read_requested_addons(inst.conf)))
            if first_boot and wanted:
                http = lib.Http(inst.url, token=token, timeout=30)
                missing = lib.wait_addons(http, wanted, deadline)
                print(f"addons  {len(wanted) - len(missing)}/{len(wanted)} from addons.cfg installed"
                      + (f"; missing: {', '.join(sorted(missing))}" if missing else ""))
                if not missing:
                    pgid, _ = lib.unstick_first_boot(rt, inst, pgid, http, deadline)
    except BaseException:
        print(f"startup failed; stopping (see {inst.console_log} and {inst.openhab_log})", file=sys.stderr)
        lib.stop_instance(inst)
        raise
    level = lib.start_level(inst)
    if level is not None and level < 100:
        print(f"note    start level {level}; 100 follows once add-ons are installed and every Thing has an "
              f"initialised handler (bin/thermite sandbox status, logs -f)")
    print(f"ready   {inst.url}  (logs: bin/thermite sandbox logs -f)")
    if args.live:
        address = deploy.lan_address()
        print(f"live    {'http://' + address + ':' + str(inst.http_port) if address else 'this host, port ' + str(inst.http_port)}"
              " from the LAN; Source selectors switch the real matrices")
    return 0


def cmd_stop(args) -> int:
    inst = lib.sandbox_instance()
    started = time.monotonic()
    with lib.InstanceLock(inst):
        stopped = lib.stop_instance(inst, timeout=args.timeout)
    if stopped:
        print(f"stopped sandbox in {time.monotonic() - started:.0f}s")
    else:
        print("sandbox is not running")
    return 0


def cmd_status(args) -> int:
    inst = lib.sandbox_instance()
    pgid = lib.running_pgid(inst)
    if not pgid:
        print("sandbox: stopped")
        return 3
    token = lib.read_token(inst.token_file) if inst.token_file.exists() else None
    code, root = lib.Http(inst.url).try_get_json("/rest/")
    version = (root or {}).get("runtimeInfo", {}).get("version") if code == 200 else None
    print(f"sandbox: running (process group {pgid}, {len(lib.group_members(pgid))} processes)")
    print(f"url:     {inst.url}  REST {'ok, runtime ' + str(version) if version else 'not answering (' + str(code) + ')'}")
    mode = lib.read_mode(inst)
    address = deploy.lan_address() if mode == lib.LIVE else None
    print(f"mode:    {describe(mode)}" + (f"; from the LAN: http://{address}:{inst.http_port}" if address else ""))
    print(f"level:   {lib.start_level(inst)}")
    print(f"token:   {inst.token_file if token else 'none'}")
    print(f"logs:    {inst.openhab_log}")
    return 0 if version else 1


def cmd_logs(args) -> int:
    inst = lib.sandbox_instance(userdata=args.userdata)
    path = inst.openhab_log
    if not path.exists() and not args.follow:
        lib.die(f"{path} does not exist yet")
    if path.exists():
        text = lib.tail_text(path, args.lines)
        if text:
            print(text, flush=True)
    if not args.follow:
        return 0
    position = path.stat().st_size if path.exists() else 0
    inode = path.stat().st_ino if path.exists() else None
    try:
        while True:
            if path.exists():
                st = path.stat()
                if st.st_ino != inode or st.st_size < position:
                    inode, position = st.st_ino, 0
                if st.st_size > position:
                    with open(path, "rb") as fh:
                        fh.seek(position)
                        chunk = fh.read()
                        position = fh.tell()
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    parser = oh_cli.command("sandbox", DESCRIPTION, EXAMPLES, NOTES)
    sub = oh_cli.actions(parser)

    p = oh_cli.action(sub, "fetch", "download, verify (sha256 from versions.json) and extract JRE + distribution",
                      examples=(("thermite sandbox fetch", "fetch the pinned runtime into build/sandbox/"),))
    p.set_defaults(func=cmd_fetch)

    p = oh_cli.action(sub, "start", "start the sandbox in the background and wait for REST", examples=(
        ("thermite sandbox start", "read-only, on 127.0.0.1:18080"),
        ("thermite sandbox start --fresh", "on an empty userdata: a first boot"),
        ("thermite sandbox start --live", "a test instance that drives the house, reachable from the LAN"),
    ))
    oh_cli.add_conf(p, None)
    p.add_argument("--userdata", type=Path, metavar="DIR", help="userdata directory (default: var/sandbox/userdata)")
    p.add_argument("--fresh", action="store_true", help="empty userdata first (only if it is below var/)")
    oh_cli.add_timeout(p, 300, "REST")
    p.add_argument("--no-token", action="store_true", help="skip creating the admin user and API token")
    p.add_argument("--live", action="store_true",
                   help="conf/ unchanged, so the matrices and the devices under control are driven for real, "
                        "and reachable from the LAN")
    p.set_defaults(func=cmd_start)

    p = oh_cli.action(sub, "reload", "refresh var/sandbox/conf from conf/, in the mode the sandbox started in",
                      examples=(("thermite sandbox reload", "after thermite generate; the running sandbox picks it up"),))
    oh_cli.add_conf(p, None)
    p.set_defaults(func=cmd_reload)

    p = oh_cli.action(sub, "stop", "stop gracefully (SIGTERM), SIGKILL after --timeout", examples=(
        ("thermite sandbox stop", "stop it"),
        ("thermite sandbox stop --timeout 30", "give Karaf longer before SIGKILL"),
    ))
    oh_cli.add_timeout(p, lib.STOP_TIMEOUT, "a graceful stop before SIGKILL")
    p.set_defaults(func=cmd_stop)

    p = oh_cli.action(sub, "status", "report whether the sandbox runs and REST answers (exit 3 if stopped)",
                      examples=(("thermite sandbox status", "process group, URL, mode, start level, token, log"),))
    p.set_defaults(func=cmd_status)

    p = oh_cli.action(sub, "logs", "show userdata/logs/openhab.log", examples=(
        ("thermite sandbox logs", "the last 50 lines"),
        ("thermite sandbox logs -f", "and keep following it"),
        ("thermite sandbox logs -f --userdata STATE_ROOT/userdata", "production's log (state_root: the host file)"),
    ))
    p.add_argument("-f", "--follow", action="store_true", help="keep following the log")
    p.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="lines to show first (default: 50)")
    p.add_argument("--userdata", type=Path, metavar="DIR", help="userdata directory (default: var/sandbox/userdata)")
    p.set_defaults(func=cmd_logs)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except lib.SandboxError as exc:
        lib.die(str(exc))
    return 2
