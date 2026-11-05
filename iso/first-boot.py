"""Thermite's first boot on a host the ISO installed; stdlib only.

The installer's plugin calls install() with the target mounted: a one-shot
unit, enabled once, that sets up an empty Thermite house as the owner and
runs thermite install with the release the image verified
(/usr/local/bin/thermite). The unit runs this file three times:

    first-boot.py prepare   as root: a temporary sudo rule for the owner
    first-boot.py run       as the owner: init, commit, thermite install
    first-boot.py finish    as root, whatever happened: the rule removed,
                            the login message written, the unit disabled

thermite install asks once, "Install? [y/N]", at a terminal: run answers
that prompt alone in a pseudo-terminal and declines anything else. Without
the Internet it waits, then leaves the commands in the login message.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import re
import select
import signal
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
SITE = "thermite-site"
#: run's exit status when the Internet never answered.
OFFLINE = 75
#: How long run waits for the Internet, and between tries.
WAIT, RETRY = 15 * 60, 20
#: thermite install's own approval prompt (oh_install.runner.confirm_run).
APPROVAL = re.compile(r"(?:^|\n)Install\? \[y/N\] $")
#: Its other questions: declined (Enter is no). A password prompt stops the run.
QUESTION = re.compile(r"\[y/N\] $|to delete it: $")
PASSWORD = re.compile(r"[Pp]assword[^\n]*: $")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")
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
PENDING_TEXT = """\
Thermite is setting itself up on this first boot: an empty house in
~/{site}, installed as {owner}. It needs the Internet and takes a while.
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
The log is /{log}. To try again (it skips what is done):
  sudo systemctl start {unit}
Or by hand, as {owner}:
  thermite init --empty ~/{site}            unless it exists
  git -C ~/{site} init && git -C ~/{site} add -A && git -C ~/{site} commit -m 'Thermite site'
  thermite install --config ~/{site}
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
    (target / STATUS).write_text(PENDING_TEXT.format(site=SITE, owner=owner, unit=UNIT, log=LOG))


# ---- prepare and finish (root) ------------------------------------------------

def pending(root: Path) -> dict:
    return json.loads((root / PENDING).read_text())


def prepare(root: Path = Path("/"), *, run=subprocess.run) -> int:
    """The owner's sudo without a password for this one unit's run. The
    owner's own wheel rule asks for one, so sudo -v would too unless any
    one rule suffices (verifypw=any)."""
    owner = pending(root)["owner"]
    rule = root / SUDOERS
    rule.unlink(missing_ok=True)
    handle = os.open(rule, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o440)
    with os.fdopen(handle, "w") as out:
        out.write(f"# Thermite's first boot; removed when it ends.\n{owner} ALL=(ALL:ALL) NOPASSWD: ALL\n"
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


def finish(root: Path = Path("/"), *, status: str | None = None, run=subprocess.run) -> int:
    """Always: the sudo rule gone, the login message, the unit disabled. A
    failed run keeps its answers, so starting the unit again retries it."""
    (root / SUDOERS).unlink(missing_ok=True)
    answers = pending(root)
    status = os.environ.get("EXIT_STATUS", "") if status is None else status
    fields = {"site": SITE, "unit": UNIT, "log": LOG, **answers}
    if status == "0":
        text = DONE_TEXT.format(**fields)
        (root / PENDING).unlink()
    else:
        reason = ("the Internet did not answer" if status == str(OFFLINE)
                  else f"it stopped with status {status or 'unknown'}")
        text = FAILED_TEXT.format(reason=reason, **fields)
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


def wait_online(log, *, online=online, sleep=time.sleep) -> bool:
    for _ in range(WAIT // RETRY):
        if online(): return True
        log.write("Waiting for the Internet...\n"); log.flush()
        sleep(RETRY)
    return online()


def drive(argv: list[str], env: dict, log, *, timeout: float = 3 * 3600) -> int:
    """argv at a pseudo-terminal: its approval answered yes, any other
    question declined, a password prompt fatal. Its exit status."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    deadline, tail, answered, status = time.monotonic() + timeout, "", False, None
    while True:
        if time.monotonic() > deadline:
            log.write("\nThermite first boot: thermite install took too long; stopped.\n")
            os.kill(pid, signal.SIGTERM)
            status = 124
            break
        ready, _, _ = select.select([fd], [], [], 5)
        if not ready: continue
        try:
            data = os.read(fd, 4096)
        except OSError:  # the terminal closes when the program ends
            break
        if not data: break
        text = data.decode("utf-8", "replace")
        log.write(text); log.flush()
        tail = (tail + ANSI.sub("", text))[-400:]
        if PASSWORD.search(tail):
            log.write("\nThermite first boot: a password was asked for; stopped.\n")
            os.kill(pid, signal.SIGTERM)
            status = 3
            break
        if not answered and APPROVAL.search(tail):
            os.write(fd, b"y\n"); answered, tail = True, ""
        elif QUESTION.search(tail):
            os.write(fd, b"\n"); tail = ""
    os.close(fd)
    _, code = os.waitpid(pid, 0)
    return status if status is not None else os.waitstatus_to_exitcode(code)


def run(home: Path | None = None, *, root: Path = Path("/"), command: str = "/usr/local/bin/thermite",
        online=online, sleep=time.sleep, drive=drive) -> int:
    answers = pending(root)
    home = Path(home or os.environ["HOME"])
    site = home / SITE
    env = {**os.environ, "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin", "TERM": "xterm-256color"}
    with (root / LOG).open("a", encoding="utf-8") as log:
        def step(argv, cwd=None) -> int:
            log.write("$ " + " ".join(argv) + "\n"); log.flush()
            return subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL).returncode
        log.write(f"Thermite first boot, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if not wait_online(log, online=online, sleep=sleep): return OFFLINE
        if not (site / "thermite-site.json").is_file():
            if step([command, "init", "--empty", str(site)]): return 1
            # The host's time zone, as the installer asked for it.
            host = site / "instance/host.yaml"
            settings = json.loads(host.read_text())
            settings["timezone"] = "Etc/UTC" if answers["timezone"] == "UTC" else answers["timezone"]
            host.write_text(json.dumps(settings, indent=2) + "\n")
        # thermite install deploys the site's committed configuration.
        if not (site / ".git").is_dir() and step(["git", "init", "-q", "-b", "main", str(site)]): return 1
        if subprocess.run(["git", "-C", str(site), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode:
            identity = (["git", "-C", str(site), "config", "user.name", answers["owner"]],
                        ["git", "-C", str(site), "config", "user.email",
                         f"{answers['owner']}@{answers['hostname']}.local"])
            for argv in (*identity, ["git", "-C", str(site), "add", "-A"],
                         ["git", "-C", str(site), "commit", "-q", "-m", "Thermite site from the installer"]):
                if step(argv): return 1
        argv = [command, "install", "--config", str(site)]
        log.write("$ " + " ".join(argv) + "\n"); log.flush()
        return drive(argv, env, log)


def main(argv: list[str]) -> int:
    if argv == ["prepare"]: return prepare()
    if argv == ["run"]: return run()
    if argv == ["finish"]: return finish()
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
