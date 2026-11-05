"""Thermite's first boot on a host the ISO installed; stdlib only.

The installer's plugin calls install() with the target mounted: a one-shot
unit, enabled once, that sets up an empty Thermite house as the owner and
runs thermite install with the release the image verified
(/usr/local/bin/thermite). The unit runs this file three times:

    first-boot.py prepare   as root: a temporary sudo rule for the owner
    first-boot.py run       as the owner: init, commit, thermite install --yes
    first-boot.py finish    as root, whatever happened: the rule removed,
                            the login message written, the unit disabled

thermite install --yes approves the installation itself and reads nothing
from stdin, which is /dev/null here; its output goes to the log and to the
journal. Without the Internet run waits, then leaves the commands in the
login message. A tmpfiles.d rule removes the sudo rule at every boot, so a
power loss during the run cannot leave it behind.
"""
from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time

UNIT = "thermite-first-boot.service"
SCRIPT = "usr/local/lib/thermite-install/first-boot.py"
#: The answers the unit needs ({owner, hostname, timezone}); it runs only
#: while this exists, and success removes it.
PENDING = "var/lib/thermite-install/first-boot.json"
#: What an interactive login shows (bootstrap.py's profile.d snippet).
STATUS = "var/lib/thermite-install/status.txt"
LOG = "var/log/thermite-install/first-boot.log"
SUDOERS = "etc/sudoers.d/90-thermite-first-boot"
#: systemd-tmpfiles removes the sudo rule early in every boot.
TMPFILES = "etc/tmpfiles.d/thermite-first-boot.conf"
SITE = "thermite-site"
#: run's exit status when the Internet never answered, and when the
#: installation ran longer than TIMEOUT.
OFFLINE, TOO_LONG = 75, 124
#: How long run waits for the Internet, and between tries.
WAIT, RETRY = 15 * 60, 20
#: How long the installation may take, all its runs together.
TIMEOUT = 3 * 3600
#: How often a running command is looked at when it writes nothing.
POLL = 5
HOSTS = (("pypi.org", 443), ("archlinux.org", 443))

UNIT_TEXT = """\
# Written by the Thermite installer: one boot's setup, then it disables itself.
[Unit]
Description=Thermite first boot: an empty house, installed as {owner}
Wants=network-online.target
After=network-online.target time-sync.target
ConditionPathExists=/{pending}

[Service]
Type=oneshot
User={owner}
WorkingDirectory=~
ExecStartPre=+/usr/bin/python3 -I -B /{script} prepare
ExecStart=/usr/bin/python3 -I -B /{script} run
ExecStopPost=+/usr/bin/python3 -I -B /{script} finish
TimeoutStartSec=4h

[Install]
WantedBy=multi-user.target
"""
TMPFILES_TEXT = """\
# Written by the Thermite installer: the first boot's temporary sudo rule
# never outlives a boot, even when the power fails while it runs.
r /{sudoers}
"""
PENDING_TEXT = """\
Thermite is setting itself up on this first boot: an empty house in
~/{site}, installed as {owner}. It needs the Internet and takes a while.
Until it ends, sudo runs any command as root for {owner} without a password.
  journalctl -fu {unit}             follows it
  less /{log}       its whole log
"""
DONE_TEXT = """\
Thermite is installed on {hostname}.local, with an empty house in ~{owner}/{site}.
  thermite status                   what runs, and the web app's address
  less /opt/thermite/FIRST-BOOT.txt the next steps
"""
FAILED_TEXT = """\
Thermite's first-boot setup did not finish: {reason}.
The log is /{log}. To try again:
  sudo systemctl start {unit}
It keeps the house it made. If the stopped run left an install open, it
first finishes or rolls back that install, then installs again.
Or by hand, as {owner}:
  thermite init --empty ~/{site}            unless it exists
  git -C ~/{site} init -b main
  git -C ~/{site} config user.name {owner}
  git -C ~/{site} config user.email {owner}@{hostname}.local
  git -C ~/{site} add -A && git -C ~/{site} commit -m 'Thermite site'
  thermite install --config ~/{site}        once more if it says it stopped
"""


def install(target: Path, media: Path, answers: dict) -> None:
    """The unit, this script and the pending answers on the target, the unit
    enabled for the next boot, and the login message saying so."""
    target = Path(target)
    owner = answers["owner"]
    script = target / SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes((Path(media) / "first-boot.py").read_bytes())
    script.chmod(0o644)
    unit = target / "etc/systemd/system" / UNIT
    unit.write_text(UNIT_TEXT.format(owner=owner, pending=PENDING, script=SCRIPT))
    wants = target / "etc/systemd/system/multi-user.target.wants" / UNIT
    wants.parent.mkdir(parents=True, exist_ok=True)
    if not wants.is_symlink(): wants.symlink_to(f"/etc/systemd/system/{UNIT}")
    pending = target / PENDING
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({name: answers[name] for name in ("owner", "hostname", "timezone")}, indent=2) + "\n")
    tmpfiles = target / TMPFILES
    tmpfiles.parent.mkdir(parents=True, exist_ok=True)
    tmpfiles.write_text(TMPFILES_TEXT.format(sudoers=SUDOERS))
    (target / STATUS).write_text(PENDING_TEXT.format(site=SITE, owner=owner, unit=UNIT, log=LOG))


# ---- prepare and finish (root) ------------------------------------------------

def pending(root: Path) -> dict:
    return json.loads((root / PENDING).read_text())


def prepare(root: Path = Path("/"), *, run=subprocess.run) -> int:
    """Any command as root through sudo, without a password, for the owner
    while this one unit runs; finish and, after a power loss, the next
    boot's tmpfiles rule remove it. The owner's own wheel rule asks for a
    password, so sudo -v would too unless any one rule suffices
    (verifypw=any)."""
    owner = pending(root)["owner"]
    rule = root / SUDOERS
    rule.unlink(missing_ok=True)
    handle = os.open(rule, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o440)
    with os.fdopen(handle, "w") as out:
        out.write(f"# Thermite's first boot: any command as root, no password; removed when it ends.\n"
                  f"{owner} ALL=(ALL:ALL) NOPASSWD: ALL\n"
                  f"Defaults:{owner} verifypw=any\n")
    if run(["visudo", "-cf", str(rule)], capture_output=True).returncode:
        rule.unlink()
        return 1
    log = root / LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    entry = owner_entry(root, owner)
    os.chown(log, entry[0], entry[1])
    return 0


def owner_entry(root: Path, owner: str) -> tuple[int, int, str]:
    for line in (root / "etc/passwd").read_text().splitlines():
        fields = line.split(":")
        if fields[0] == owner: return int(fields[2]), int(fields[3]), fields[5]
    raise KeyError(owner)


def reason(status: str, code: str) -> str:
    """Why run stopped, from systemd's EXIT_STATUS and EXIT_CODE."""
    if code in ("killed", "dumped"):
        return f"it was stopped by signal {status or 'unknown'}, as a shutdown or restart does"
    if status == str(OFFLINE): return f"the Internet did not answer in {WAIT // 60} minutes"
    if status == str(TOO_LONG): return f"the installation ran longer than {TIMEOUT // 3600} hours and was stopped"
    return f"it stopped with status {status or 'unknown'}; the log says why"


def finish(root: Path = Path("/"), *, status: str | None = None, code: str | None = None, run=subprocess.run) -> int:
    """Always: the sudo rule gone, the login message, the unit disabled. A
    failed run keeps its answers, so starting the unit again retries it."""
    (root / SUDOERS).unlink(missing_ok=True)
    answers = pending(root)
    status = os.environ.get("EXIT_STATUS", "") if status is None else status
    code = os.environ.get("EXIT_CODE", "") if code is None else code
    fields = {"site": SITE, "unit": UNIT, "log": LOG, **answers}
    if status == "0" and code in ("", "exited"):
        text = DONE_TEXT.format(**fields)
        (root / PENDING).unlink()
    else:
        text = FAILED_TEXT.format(reason=reason(status, code), **fields)
    (root / STATUS).write_text(text)
    run(["systemctl", "disable", UNIT], capture_output=True)
    return 0


# ---- run (the owner) ------------------------------------------------------------

def online(hosts=HOSTS) -> bool:
    for host in hosts:
        try:
            with socket.create_connection(host, timeout=10): return True
        except OSError:
            continue
    return False


def wait_online(say, *, online=online, sleep=time.sleep) -> bool:
    for _ in range(WAIT // RETRY):
        if online(): return True
        say("Waiting for the Internet...\n")
        sleep(RETRY)
    return online()


def tee(argv: list[str], env: dict, say, deadline: float, cwd=None) -> int:
    """argv with stdin at /dev/null and its output said as it comes; its
    exit status, or TOO_LONG once deadline passes. It ends when argv does,
    even if a process argv started still holds its output open."""
    process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    decode = codecs.getincrementaldecoder("utf-8")("replace")
    output = process.stdout.fileno()
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([output], [], [], POLL)
            if ready:
                data = os.read(output, 65536)
                if not data: break
                say(decode.decode(data))
            elif process.poll() is not None:
                break
        else:
            say("\nThermite first boot: the installation took too long; stopped.\n")
            process.terminate()
            try: process.wait(30)
            except subprocess.TimeoutExpired: process.kill()
            return TOO_LONG
        say(decode.decode(b"", final=True))
        try:
            return process.wait(max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            return TOO_LONG
    finally:
        process.stdout.close()
        if process.poll() is None: process.wait()


def open_transaction(command: str, site: Path, env: dict, say) -> str | None:
    """thermite status --json's open_transaction: an install an earlier,
    stopped run left open (None when there is none, or no report)."""
    try:
        completed = subprocess.run([command, "status", "--json", "--config", str(site)], env=env,
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        return report.get("open_transaction") if isinstance(report, dict) else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        say("thermite status --json gave no report; installing without it.\n")
        return None


def run(home: Path | None = None, *, root: Path = Path("/"), command: str = "/usr/local/bin/thermite",
        online=online, sleep=time.sleep, out=None, timeout: float = TIMEOUT) -> int:
    answers = pending(root)
    home = Path(home or os.environ["HOME"])
    site = home / SITE
    # Nothing here is a terminal: plain output, in English, for the log.
    env = {**os.environ, "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin", "TERM": "dumb", "LC_ALL": "C"}
    out = sys.stdout if out is None else out
    deadline = time.monotonic() + timeout
    with (root / LOG).open("a", encoding="utf-8") as log:
        def say(text: str) -> None:
            for stream in (log, out):  # the log, and the journal
                stream.write(text); stream.flush()

        def step(argv) -> int:
            say("$ " + " ".join(argv) + "\n")
            return tee(argv, env, say, deadline)
        say(f"Thermite first boot, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if not wait_online(say, online=online, sleep=sleep): return OFFLINE
        if not (site / "thermite-site.json").is_file():
            if step([command, "init", "--empty", str(site)]): return 1
            # The host's time zone, as the installer asked for it.
            host = site / "instance/host.yaml"
            settings = json.loads(host.read_text())
            settings["timezone"] = "Etc/UTC" if answers["timezone"] == "UTC" else answers["timezone"]
            host.write_text(json.dumps(settings, indent=2) + "\n")
        # thermite install deploys the site's committed configuration.
        if not (site / ".git").is_dir() and step(["git", "init", "-q", "-b", "main", str(site)]): return 1
        if subprocess.run(["git", "-C", str(site), "rev-parse", "--verify", "-q", "HEAD"], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True).returncode:
            identity = (["git", "-C", str(site), "config", "user.name", answers["owner"]],
                        ["git", "-C", str(site), "config", "user.email",
                         f"{answers['owner']}@{answers['hostname']}.local"])
            for argv in (*identity, ["git", "-C", str(site), "add", "-A"],
                         ["git", "-C", str(site), "commit", "-q", "-m", "Thermite site from the installer"]):
                if step(argv): return 1
        install = [command, "install", "--yes", "--config", str(site)]
        # An install a stopped run left open (a restart, a power loss): the
        # first thermite install finishes or rolls it back and stops there,
        # by design; the second installs.
        left = open_transaction(command, site, env, say)
        if left:
            say(f"Install transaction {left} of a stopped run is still open: thermite install resolves it "
                "first and stops, then runs again to install.\n")
            if step(install) == TOO_LONG: return TOO_LONG
        return step(install)


def main(argv: list[str]) -> int:
    if argv == ["prepare"]: return prepare()
    if argv == ["run"]: return run()
    if argv == ["finish"]: return finish()
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
